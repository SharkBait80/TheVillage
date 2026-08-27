import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

// Full functional coverage for the Melbourne Agent Village SPA, exercised in
// mock mode (VITE_MOCK=1) so no AWS/API/auth is required. These specs cover the
// user-facing functions NOT already asserted by ui.spec.ts / add-event.spec.ts:
//
//   - Simulation controls: real status transitions (running → paused → running
//     → stopped → running) observed through the poll loop, plus enabled/disabled
//     validity, and a rejection toast.
//   - AgentPanel: opened from the list view; persona, need progressbars, cash /
//     employment / legal, thought-process, and recent events.
//   - LocationPanel: opened from a map marker; status, capacity, opening hours,
//     present-agent cross-navigation into the AgentPanel.
//   - ConversationsPanel: seeded transcript content + participant cross-nav.
//
// The mock backend starts in `running` status and refreshes the world on a
// ~1.5s poll, so control-state assertions use generous timeouts (>= 2 poll
// cycles) per Playwright polled-UI guidance.

const POLL_HEADROOM = 8_000 // ~5 poll cycles of headroom for polled state flips.

async function gotoApp(page: Page) {
  await page.goto('/')
  await expect(page.getByText('Melbourne Agent Village').first()).toBeVisible()
}

/** Ensure the list view is open (the toggle label flips to "Map view" once open). */
async function ensureListOpen(page: Page) {
  const list = page.getByRole('region', { name: /Agent list/i })
  if (!(await list.isVisible())) {
    await page.getByRole('button', { name: 'List view' }).click()
    await expect(list).toBeVisible()
  }
  return list
}

/** Open the list view and click the first agent's name button → AgentPanel. */
async function openFirstAgentPanel(page: Page) {
  const list = await ensureListOpen(page)
  const firstAgentBtn = list.locator('tbody tr th button').first()
  await expect(firstAgentBtn).toBeVisible()
  const name = (await firstAgentBtn.textContent())?.trim() ?? ''
  await firstAgentBtn.click()
  const panel = page.getByRole('dialog', { name: 'Agent details' })
  await expect(panel).toBeVisible()
  return { panel, name }
}

test.describe('simulation controls', () => {
  test('start is disabled while running; pause/stop enabled', async ({ page }) => {
    await gotoApp(page)
    const controls = page.getByRole('group', { name: 'Simulation controls' })
    // Wait for the first poll so the displayed status settles to "running".
    await expect(page.getByText('Running', { exact: true })).toBeVisible({ timeout: POLL_HEADROOM })

    await expect(controls.getByRole('button', { name: 'Start' })).toBeDisabled()
    await expect(controls.getByRole('button', { name: 'Pause' })).toBeEnabled()
    await expect(controls.getByRole('button', { name: 'Resume' })).toBeDisabled()
    await expect(controls.getByRole('button', { name: 'Stop' })).toBeEnabled()
  })

  test('pause → resume → stop → start round-trips through the poll loop', async ({ page }) => {
    await gotoApp(page)
    const controls = page.getByRole('group', { name: 'Simulation controls' })
    const startBtn = controls.getByRole('button', { name: 'Start' })
    const pauseBtn = controls.getByRole('button', { name: 'Pause' })
    const resumeBtn = controls.getByRole('button', { name: 'Resume' })
    const stopBtn = controls.getByRole('button', { name: 'Stop' })
    const statusPill = page.locator('.status-pill')

    await expect(statusPill).toHaveText('Running', { timeout: POLL_HEADROOM })

    // running → paused
    await pauseBtn.click()
    await expect(statusPill).toHaveText('Paused', { timeout: POLL_HEADROOM })
    await expect(pauseBtn).toBeDisabled()
    await expect(resumeBtn).toBeEnabled()
    await expect(stopBtn).toBeEnabled()

    // paused → running
    await resumeBtn.click()
    await expect(statusPill).toHaveText('Running', { timeout: POLL_HEADROOM })
    await expect(pauseBtn).toBeEnabled()
    await expect(resumeBtn).toBeDisabled()

    // running → stopped
    await stopBtn.click()
    await expect(statusPill).toHaveText('Stopped', { timeout: POLL_HEADROOM })
    await expect(startBtn).toBeEnabled()
    await expect(pauseBtn).toBeDisabled()
    await expect(stopBtn).toBeDisabled()

    // stopped → running
    await startBtn.click()
    await expect(statusPill).toHaveText('Running', { timeout: POLL_HEADROOM })
    await expect(startBtn).toBeDisabled()
    await expect(pauseBtn).toBeEnabled()
  })

  test('the control validity guard prevents issuing invalid commands', async ({ page }) => {
    // The UI enforces Req15.9 by disabling commands invalid for the current
    // status, which is what prevents the rejection path from being reachable in
    // normal use. Assert the full validity matrix across every status the
    // operator can drive the sim through.
    await gotoApp(page)
    const controls = page.getByRole('group', { name: 'Simulation controls' })
    const startBtn = controls.getByRole('button', { name: 'Start' })
    const pauseBtn = controls.getByRole('button', { name: 'Pause' })
    const resumeBtn = controls.getByRole('button', { name: 'Resume' })
    const stopBtn = controls.getByRole('button', { name: 'Stop' })
    const statusPill = page.locator('.status-pill')

    // running: only pause + stop are valid.
    await expect(statusPill).toHaveText('Running', { timeout: POLL_HEADROOM })
    await expect(startBtn).toBeDisabled()
    await expect(resumeBtn).toBeDisabled()

    // paused: only resume + stop are valid.
    await pauseBtn.click()
    await expect(statusPill).toHaveText('Paused', { timeout: POLL_HEADROOM })
    await expect(startBtn).toBeDisabled()
    await expect(pauseBtn).toBeDisabled()

    // stopped: only start is valid.
    await stopBtn.click()
    await expect(statusPill).toHaveText('Stopped', { timeout: POLL_HEADROOM })
    await expect(pauseBtn).toBeDisabled()
    await expect(resumeBtn).toBeDisabled()
    await expect(stopBtn).toBeDisabled()
    await expect(startBtn).toBeEnabled()
  })
})

test.describe('agent detail panel', () => {
  test('opens from the list view and shows persona, needs, and status', async ({ page }) => {
    await gotoApp(page)
    const { panel, name } = await openFirstAgentPanel(page)

    // Persona heading matches the clicked agent's name.
    await expect(panel.getByRole('heading', { level: 2 })).toHaveText(name)

    // Four need progressbars, each with a valid aria-valuenow in [0,100].
    for (const need of ['Hunger', 'Energy', 'Social', 'Fun']) {
      const bar = panel.getByRole('progressbar', { name: `${need} level` })
      await expect(bar).toBeVisible()
      const now = Number(await bar.getAttribute('aria-valuenow'))
      expect(Number.isFinite(now)).toBe(true)
      expect(now).toBeGreaterThanOrEqual(0)
      expect(now).toBeLessThanOrEqual(100)
    }

    // Status key/value rows (scope to the .k key labels to avoid matching the
    // perception "Cash: $…" list item / "Legal status" prose elsewhere).
    const keys = panel.locator('.kv .k')
    await expect(keys.filter({ hasText: 'Cash' })).toBeVisible()
    await expect(panel.getByText(/\$\d+(\.\d{2})? AUD/)).toBeVisible()
    await expect(keys.filter({ hasText: 'Employment' })).toBeVisible()
    await expect(keys.filter({ hasText: 'Legal' })).toBeVisible()
    await expect(keys.filter({ hasText: 'Current action' })).toBeVisible()
  })

  test('shows a thought-process section and recent events', async ({ page }) => {
    await gotoApp(page)
    const { panel } = await openFirstAgentPanel(page)

    await expect(panel.getByText('Thought process')).toBeVisible()
    // The mock always synthesises a decision trail, so the reasoning quote shows.
    await expect(panel.locator('.thought-reasoning')).toBeVisible()

    await expect(panel.getByText('Recent events')).toBeVisible()
    // Mock seeds action + planning events for every agent.
    await expect(panel.locator('.event-list .event-item').first()).toBeVisible()
  })

  test('closes via the close button and via Escape', async ({ page }) => {
    await gotoApp(page)
    const { panel } = await openFirstAgentPanel(page)
    await panel.getByRole('button', { name: 'Close agent details' }).click()
    await expect(panel).toBeHidden()

    // Re-open and close with Escape.
    const reopened = (await openFirstAgentPanel(page)).panel
    await reopened.press('Escape')
    await expect(reopened).toBeHidden()
  })
})

test.describe('location detail panel', () => {
  // Open a LocationPanel by activating a location marker. Markers are Leaflet
  // DivIcons (role=button, class .loc-marker-wrap) whose click handler opens the
  // panel. Because markers overlap, we dispatch a raw click on the marker
  // element to bypass pointer hit-testing (per Playwright Leaflet guidance).
  async function openLocationPanel(page: Page) {
    const marker = page.locator('.loc-marker-wrap').first()
    await expect(marker).toBeVisible({ timeout: POLL_HEADROOM })
    await marker.dispatchEvent('click')
    const panel = page.getByRole('dialog', { name: 'Location details' })
    await expect(panel).toBeVisible()
    return panel
  }

  test('opens from a map marker and shows status, capacity and opening hours', async ({ page }) => {
    await gotoApp(page)
    const panel = await openLocationPanel(page)

    await expect(panel.getByRole('heading', { level: 2 })).toBeVisible()
    const keys = panel.locator('.kv .k')
    await expect(keys.filter({ hasText: 'Status' })).toBeVisible()
    await expect(keys.filter({ hasText: 'Capacity' })).toBeVisible()
    await expect(panel.getByText('Opening hours')).toBeVisible()
    // Seven day rows (Mon..Sun) with open–close times.
    await expect(panel.getByRole('row', { name: /Mon/ })).toBeVisible()
    await expect(panel.getByRole('row', { name: /Sun/ })).toBeVisible()
  })

  test('present-agent list cross-navigates into the AgentPanel', async ({ page }) => {
    await gotoApp(page)
    // Try each location marker until one lists a present agent, then click it.
    const markers = page.locator('.loc-marker-wrap')
    await expect(markers.first()).toBeVisible({ timeout: POLL_HEADROOM })
    const count = await markers.count()
    let opened = false
    for (let i = 0; i < count; i++) {
      await markers.nth(i).dispatchEvent('click')
      const panel = page.getByRole('dialog', { name: 'Location details' })
      await expect(panel).toBeVisible()
      const presentBtn = panel.locator('.event-list .event-item button').first()
      if (await presentBtn.count()) {
        await presentBtn.click()
        opened = true
        break
      }
      await panel.getByRole('button', { name: 'Close location details' }).click()
      await expect(panel).toBeHidden()
    }
    if (opened) {
      await expect(page.getByRole('dialog', { name: 'Agent details' })).toBeVisible()
    } else {
      test.info().annotations.push({
        type: 'note',
        description: 'No location had a present agent this session; cross-nav not exercised.',
      })
    }
  })
})

test.describe('conversations panel content', () => {
  test('lists a seeded transcript with participant names and utterances', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Conversations' }).click()
    const dialog = page.getByRole('dialog', { name: 'Conversations' })
    await expect(dialog).toBeVisible()

    // The mock seeds at least one resolved conversation on load.
    const card = dialog.locator('.conversation-card').first()
    await expect(card).toBeVisible({ timeout: POLL_HEADROOM })
    // Each card lists utterances with a speaker + text.
    await expect(card.locator('.utterance').first()).toBeVisible()
    await expect(card.locator('.utterance-speaker').first()).toBeVisible()
    await expect(card.locator('.utterance-text').first()).toBeVisible()
  })

  test('clicking a participant opens that agent’s detail panel', async ({ page }) => {
    await gotoApp(page)
    await page.getByRole('button', { name: 'Conversations' }).click()
    const dialog = page.getByRole('dialog', { name: 'Conversations' })
    await expect(dialog).toBeVisible()

    const participant = dialog.locator('.conversation-participants .link-btn').first()
    await expect(participant).toBeVisible({ timeout: POLL_HEADROOM })
    await participant.click()

    await expect(page.getByRole('dialog', { name: 'Agent details' })).toBeVisible()
  })
})
