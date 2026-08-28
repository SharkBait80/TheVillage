import { test, expect } from '@playwright/test'
import type { Page, Route } from '@playwright/test'

// End-to-end coverage for the two auth fixes:
//   1. Session persistence across reload — a persisted Cognito refresh token is
//      exchanged (REFRESH_TOKEN_AUTH) for fresh tokens on load, so a page
//      refresh does NOT bounce the operator back to the login screen.
//   2. In-UI password change — the "Change password" control opens a dialog
//      that calls the Cognito ChangePassword API.
//
// This runs against a NON-mock production bundle (so src/auth.ts runs for real)
// with all Cognito + Simulation API traffic stubbed via page.route(). No AWS
// backend is required.

const COGNITO_HOST = 'https://cognito-idp.ap-southeast-2.amazonaws.com/'

/** Base64url without padding — matches what auth.ts decodeJwtPayload expects. */
function b64url(obj: unknown): string {
  const json = JSON.stringify(obj)
  const b64 = Buffer.from(json, 'utf8').toString('base64')
  return b64.replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
}

/** Build a fake (unsigned) JWT whose exp is `hoursAhead` hours in the future. */
function fakeJwt(hoursAhead = 1): string {
  const header = b64url({ alg: 'none', typ: 'JWT' })
  const exp = Math.floor(Date.now() / 1000) + hoursAhead * 3600
  const payload = b64url({ sub: 'operator', exp, token_use: 'id' })
  return `${header}.${payload}.sig`
}

interface CognitoStubState {
  initiateAuthFlows: string[]
  changePasswordBodies: Array<Record<string, unknown>>
  /** When set, ChangePassword responds with this error type instead of 200. */
  changePasswordError?: { status: number; type: string; message?: string }
  /** When true, REFRESH_TOKEN_AUTH is rejected (simulates expired session). */
  rejectRefresh?: boolean
}

/**
 * Install stubs for Cognito IDP + the Simulation API. Returns mutable state so
 * tests can assert on / drive the stub behaviour.
 */
async function installStubs(page: Page): Promise<CognitoStubState> {
  const state: CognitoStubState = { initiateAuthFlows: [], changePasswordBodies: [] }

  await page.route(COGNITO_HOST, async (route: Route) => {
    const req = route.request()
    const target = req.headers()['x-amz-target'] ?? ''
    const body = JSON.parse(req.postData() ?? '{}') as Record<string, any>

    if (target.endsWith('InitiateAuth')) {
      state.initiateAuthFlows.push(body.AuthFlow)
      if (body.AuthFlow === 'REFRESH_TOKEN_AUTH' && state.rejectRefresh) {
        return route.fulfill({
          status: 400,
          contentType: 'application/x-amz-json-1.1',
          body: JSON.stringify({ __type: 'NotAuthorizedException', message: 'Refresh Token has expired.' }),
        })
      }
      const AuthenticationResult: Record<string, unknown> = {
        IdToken: fakeJwt(1),
        AccessToken: fakeJwt(1),
        ExpiresIn: 3600,
      }
      // USER_PASSWORD_AUTH returns a refresh token; REFRESH_TOKEN_AUTH does not.
      if (body.AuthFlow === 'USER_PASSWORD_AUTH') {
        AuthenticationResult.RefreshToken = 'fake-refresh-token'
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/x-amz-json-1.1',
        body: JSON.stringify({ AuthenticationResult }),
      })
    }

    if (target.endsWith('ChangePassword')) {
      state.changePasswordBodies.push(body)
      if (state.changePasswordError) {
        return route.fulfill({
          status: state.changePasswordError.status,
          contentType: 'application/x-amz-json-1.1',
          body: JSON.stringify({
            __type: state.changePasswordError.type,
            message: state.changePasswordError.message ?? '',
          }),
        })
      }
      return route.fulfill({ status: 200, contentType: 'application/x-amz-json-1.1', body: '{}' })
    }

    return route.fulfill({ status: 400, body: '{}' })
  })

  // Stub the Simulation API so the app can mount past the auth gate.
  await page.route('**/v1/sim/**', async (route: Route) => {
    const url = route.request().url()
    if (url.includes('/locations')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, data: { locations: [] } }) })
    }
    // /state and anything else: minimal valid envelope.
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        data: { status: 'stopped', simTime: null, accel: 1, agents: [], conversations: [] },
      }),
    })
  })

  return state
}

/** Sign in through the login screen and wait for the app shell to mount. */
async function signIn(page: Page) {
  await page.goto('/')
  // No stored session yet → login screen.
  await page.getByLabel('Username').fill('operator')
  await page.getByLabel('Password').fill('CurrentPass1!')
  await page.getByRole('button', { name: 'Sign in' }).click()
  // Header controls confirm the app mounted (auth gate passed).
  await expect(page.getByRole('button', { name: 'Change password' })).toBeVisible()
}

test.describe('session persistence (refresh token)', () => {
  test('a page reload resumes the session without re-login', async ({ page }) => {
    const state = await installStubs(page)
    await signIn(page)

    // A refresh token should have been persisted from USER_PASSWORD_AUTH.
    const stored = await page.evaluate(() => window.localStorage.getItem('mav.cognito.refreshToken'))
    expect(stored).toBe('fake-refresh-token')

    // Reload: the app should silently exchange the refresh token and mount
    // directly, NOT show the login screen.
    await page.reload()
    await expect(page.getByRole('button', { name: 'Change password' })).toBeVisible()
    await expect(page.getByLabel('Username')).toHaveCount(0)

    // The reload used the REFRESH_TOKEN_AUTH flow.
    expect(state.initiateAuthFlows).toContain('REFRESH_TOKEN_AUTH')
  })

  test('an expired refresh token falls back to the login screen', async ({ page }) => {
    const state = await installStubs(page)
    await signIn(page)
    state.rejectRefresh = true

    await page.reload()
    // Rejected refresh → login screen returns; stored token is cleared.
    await expect(page.getByLabel('Username')).toBeVisible()
    const stored = await page.evaluate(() => window.localStorage.getItem('mav.cognito.refreshToken'))
    expect(stored).toBeNull()
  })
})

test.describe('change password', () => {
  test('operator can change their password via the UI', async ({ page }) => {
    const state = await installStubs(page)
    await signIn(page)

    await page.getByRole('button', { name: 'Change password' }).click()
    const dialog = page.getByRole('dialog', { name: 'Change password' })
    await expect(dialog).toBeVisible()

    await dialog.getByLabel('Current password').fill('CurrentPass1!')
    await dialog.getByLabel('New password', { exact: true }).fill('BrandNewPass2!')
    await dialog.getByLabel('Confirm new password').fill('BrandNewPass2!')
    await dialog.locator('button[type="submit"]').click()

    await expect(dialog.getByText('Password changed')).toBeVisible()
    expect(state.changePasswordBodies).toHaveLength(1)
    expect(state.changePasswordBodies[0]).toMatchObject({
      PreviousPassword: 'CurrentPass1!',
      ProposedPassword: 'BrandNewPass2!',
    })
    expect(typeof state.changePasswordBodies[0].AccessToken).toBe('string')
  })

  test('client-side validation blocks a weak new password', async ({ page }) => {
    const state = await installStubs(page)
    await signIn(page)

    await page.getByRole('button', { name: 'Change password' }).click()
    const dialog = page.getByRole('dialog', { name: 'Change password' })
    await dialog.getByLabel('Current password').fill('CurrentPass1!')
    await dialog.getByLabel('New password', { exact: true }).fill('short')
    await dialog.getByLabel('Confirm new password').fill('short')
    await dialog.locator('button[type="submit"]').click()

    await expect(dialog.getByRole('alert')).toContainText('at least 12 characters')
    // No network call should have been made for an invalid password.
    expect(state.changePasswordBodies).toHaveLength(0)
  })

  test('surfaces a wrong current password from Cognito', async ({ page }) => {
    const state = await installStubs(page)
    await signIn(page)
    state.changePasswordError = { status: 400, type: 'NotAuthorizedException' }

    await page.getByRole('button', { name: 'Change password' }).click()
    const dialog = page.getByRole('dialog', { name: 'Change password' })
    await dialog.getByLabel('Current password').fill('WrongPass1!')
    await dialog.getByLabel('New password', { exact: true }).fill('BrandNewPass2!')
    await dialog.getByLabel('Confirm new password').fill('BrandNewPass2!')
    await dialog.locator('button[type="submit"]').click()

    await expect(dialog.getByRole('alert')).toContainText('Current password is incorrect')
  })
})
