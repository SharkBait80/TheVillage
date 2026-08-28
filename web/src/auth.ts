// Cognito authentication (in-app, always-fresh JWT).
//
// Baking a static token into the build fails because Cognito ID tokens expire
// in ~1h. Instead we authenticate at runtime against the Cognito Identity
// Provider REST API (InitiateAuth) using plain fetch — no
// `amazon-cognito-identity-js` dependency.
//
// Token handling & persistence strategy:
//   - IdToken / AccessToken (both short-lived JWTs) are kept IN MEMORY ONLY.
//     A persisted bearer token is a prime XSS exfiltration target, so we never
//     write them to localStorage.
//   - The RefreshToken (long-lived, 30d, opaque — NOT a bearer token our API
//     accepts) is persisted to localStorage so the session survives a page
//     reload. On load we silently exchange it for fresh Id/Access tokens via
//     the REFRESH_TOKEN_AUTH flow, so the operator is no longer forced to
//     re-enter credentials on every refresh (see Finding 7 — this replaces the
//     previous approach of retaining the plaintext password in module memory).
//   - The IdToken authorizes the API Gateway JWT authorizer
//     (`Authorization: Bearer <idToken>`); the AccessToken (which carries the
//     `aws.cognito.signin.user.admin` scope) authorizes ChangePassword.
//
// Config comes from Vite env:
//   VITE_COGNITO_REGION     e.g. ap-southeast-2
//   VITE_COGNITO_CLIENT_ID  App Client id (no client secret)
//   VITE_OPERATOR_USER      optional — auto-login username
//   VITE_OPERATOR_PASS      optional — auto-login password

const REGION = import.meta.env.VITE_COGNITO_REGION ?? ''
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID ?? ''
const OPERATOR_USER = import.meta.env.VITE_OPERATOR_USER ?? ''
const OPERATOR_PASS = import.meta.env.VITE_OPERATOR_PASS ?? ''

/** Re-auth when within this many ms of expiry. */
const REFRESH_SKEW_MS = 5 * 60 * 1000

/** localStorage key for the persisted (opaque) Cognito refresh token. */
const REFRESH_TOKEN_KEY = 'mav.cognito.refreshToken'

export const authConfig = {
  region: REGION,
  clientId: CLIENT_ID,
  hasOperatorCreds: Boolean(OPERATOR_USER && OPERATOR_PASS),
  configured: Boolean(REGION && CLIENT_ID),
}

/** Thrown when login fails; carries a human-friendly message. */
export class AuthError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AuthError'
  }
}

// In-memory copies of the current short-lived JWTs (source of truth during a
// session). Intentionally NOT persisted to localStorage: a persisted bearer
// token is a prime XSS exfiltration target (see security review Finding 2).
// They are lost on reload; refreshFromStoredToken()/ensureAuth() transparently
// mint new ones from the persisted refresh token.
let idToken: string | null = null
// Access token carries the `aws.cognito.signin.user.admin` scope required by
// ChangePassword. In-memory only, same rationale as idToken.
let accessToken: string | null = null
// Guards against overlapping re-auth calls.
let inFlight: Promise<string> | null = null

/** Safe localStorage access (may be unavailable/blocked; never throw). */
function readRefreshToken(): string | null {
  try {
    return window.localStorage.getItem(REFRESH_TOKEN_KEY)
  } catch {
    return null
  }
}

function writeRefreshToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(REFRESH_TOKEN_KEY, token)
    else window.localStorage.removeItem(REFRESH_TOKEN_KEY)
  } catch {
    // Storage unavailable (private mode / blocked): degrade gracefully — the
    // session simply won't survive a reload, matching the old behaviour.
  }
}

function storeTokens(next: { idToken?: string | null; accessToken?: string | null }): void {
  // In-memory only — no localStorage. See the note on `idToken` above.
  if (next.idToken !== undefined) idToken = next.idToken
  if (next.accessToken !== undefined) accessToken = next.accessToken
}

/** Base64url-decode + parse a JWT's payload. Returns null when unparseable. */
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    while (b64.length % 4) b64 += '='
    const json = atob(b64)
    return JSON.parse(json) as Record<string, unknown>
  } catch {
    return null
  }
}

/** Epoch ms at which the token expires, or null if unknown. */
function tokenExpiryMs(token: string): number | null {
  const payload = decodeJwtPayload(token)
  const exp = payload?.exp
  if (typeof exp !== 'number') return null
  return exp * 1000
}

/** True when the token is missing or within the refresh skew of expiry. */
function isTokenFresh(token: string | null): boolean {
  if (!token) return false
  const expMs = tokenExpiryMs(token)
  if (expMs == null) return false
  return Date.now() < expMs - REFRESH_SKEW_MS
}

/** Current IdToken (may be stale/expired — callers should ensureAuth first). */
export function getIdToken(): string | null {
  return idToken
}

/** Current AccessToken (used for ChangePassword). May be null/stale. */
export function getAccessToken(): string | null {
  return accessToken
}

/** True when a persisted refresh token exists (session may be resumable). */
export function hasStoredSession(): boolean {
  return Boolean(readRefreshToken())
}

/** True when we currently hold a token that is not within the refresh skew. */
export function hasValidToken(): boolean {
  return isTokenFresh(idToken)
}

/**
 * Authenticate with Cognito (USER_PASSWORD_AUTH) and store the resulting
 * IdToken. Throws AuthError on failure.
 */
export async function login(username: string, password: string): Promise<string> {
  if (!authConfig.configured) {
    throw new AuthError('Cognito is not configured (missing VITE_COGNITO_REGION / VITE_COGNITO_CLIENT_ID).')
  }
  const endpoint = `https://cognito-idp.${REGION}.amazonaws.com/`
  let res: Response
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
      },
      body: JSON.stringify({
        AuthFlow: 'USER_PASSWORD_AUTH',
        ClientId: CLIENT_ID,
        AuthParameters: { USERNAME: username, PASSWORD: password },
      }),
    })
  } catch {
    throw new AuthError('Could not reach the sign-in service. Check your connection.')
  }

  let data: {
    AuthenticationResult?: { IdToken?: string; AccessToken?: string; RefreshToken?: string; ExpiresIn?: number }
    ChallengeName?: string
    message?: string
    __type?: string
  }
  try {
    data = await res.json()
  } catch {
    throw new AuthError(`Sign-in failed (${res.status}).`)
  }

  if (!res.ok) {
    const type = (data.__type ?? '').split('#').pop() ?? ''
    if (type === 'NotAuthorizedException' || type === 'UserNotFoundException') {
      throw new AuthError('Incorrect username or password.')
    }
    throw new AuthError(data.message || type || `Sign-in failed (${res.status}).`)
  }

  if (data.ChallengeName) {
    throw new AuthError(`Additional sign-in step required (${data.ChallengeName}). Not supported here.`)
  }

  const token = data.AuthenticationResult?.IdToken
  if (!token) {
    throw new AuthError('Sign-in succeeded but no token was returned.')
  }

  storeTokens({ idToken: token, accessToken: data.AuthenticationResult?.AccessToken ?? null })
  // Persist the long-lived refresh token so the session survives a reload.
  const refreshToken = data.AuthenticationResult?.RefreshToken
  if (refreshToken) writeRefreshToken(refreshToken)
  return token
}

/**
 * Exchange the persisted refresh token for fresh Id/Access tokens via the
 * REFRESH_TOKEN_AUTH flow. Returns the new IdToken. Throws AuthError when there
 * is no stored refresh token or Cognito rejects it (expired/revoked), clearing
 * the stale token so callers fall back to the login screen.
 *
 * Note: REFRESH_TOKEN_AUTH does NOT return a new refresh token — we keep using
 * the stored one until it expires (~30d).
 */
export async function refreshFromStoredToken(): Promise<string> {
  if (!authConfig.configured) {
    throw new AuthError('Cognito is not configured (missing VITE_COGNITO_REGION / VITE_COGNITO_CLIENT_ID).')
  }
  const refreshToken = readRefreshToken()
  if (!refreshToken) {
    throw new AuthError('No stored session.')
  }

  const endpoint = `https://cognito-idp.${REGION}.amazonaws.com/`
  let res: Response
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth',
      },
      body: JSON.stringify({
        AuthFlow: 'REFRESH_TOKEN_AUTH',
        ClientId: CLIENT_ID,
        AuthParameters: { REFRESH_TOKEN: refreshToken },
      }),
    })
  } catch {
    throw new AuthError('Could not reach the sign-in service. Check your connection.')
  }

  let data: {
    AuthenticationResult?: { IdToken?: string; AccessToken?: string }
    message?: string
    __type?: string
  }
  try {
    data = await res.json()
  } catch {
    throw new AuthError(`Session refresh failed (${res.status}).`)
  }

  if (!res.ok) {
    // The stored refresh token is no longer usable (expired/revoked): drop it
    // so the app cleanly falls back to interactive login.
    writeRefreshToken(null)
    const type = (data.__type ?? '').split('#').pop() ?? ''
    if (type === 'NotAuthorizedException') {
      throw new AuthError('Session expired. Please sign in again.')
    }
    throw new AuthError(data.message || type || `Session refresh failed (${res.status}).`)
  }

  const token = data.AuthenticationResult?.IdToken
  if (!token) {
    writeRefreshToken(null)
    throw new AuthError('Session refresh returned no token.')
  }
  storeTokens({ idToken: token, accessToken: data.AuthenticationResult?.AccessToken ?? null })
  return token
}

/**
 * Ensure we hold a fresh IdToken. Returns it, silently minting new tokens from
 * the persisted refresh token when the in-memory token is missing/stale, and
 * falling back to the configured operator credentials only if no refresh token
 * is available. Concurrent callers share a single in-flight request. Throws
 * AuthError when the session cannot be refreshed.
 */
export async function ensureAuth(): Promise<string> {
  if (isTokenFresh(idToken)) return idToken as string
  if (inFlight) return inFlight

  const run = async (): Promise<string> => {
    // Prefer the refresh-token flow (no plaintext credentials retained).
    if (hasStoredSession()) {
      try {
        return await refreshFromStoredToken()
      } catch {
        // Fall through to operator creds below (if any) — otherwise rethrow.
      }
    }
    if (OPERATOR_USER && OPERATOR_PASS) {
      return login(OPERATOR_USER, OPERATOR_PASS)
    }
    throw new AuthError('Session expired. Please sign in again.')
  }

  inFlight = run().finally(() => {
    inFlight = null
  })
  return inFlight
}

/**
 * Attempt an automatic sign-in on app load. First tries to resume a persisted
 * session via the refresh token (so a page reload does NOT force a re-login),
 * then falls back to configured operator credentials. Resolves true on success,
 * false otherwise (never throws) so the UI can show the login screen.
 */
export async function tryAutoLogin(): Promise<boolean> {
  if (isTokenFresh(idToken)) return true
  if (hasStoredSession()) {
    try {
      await refreshFromStoredToken()
      return true
    } catch {
      // Stored token was rejected/cleared; try operator creds next.
    }
  }
  if (!authConfig.hasOperatorCreds) return false
  try {
    await login(OPERATOR_USER, OPERATOR_PASS)
    return true
  } catch {
    return false
  }
}

/**
 * Change the signed-in operator's password via the Cognito ChangePassword API,
 * authorized by the in-memory AccessToken (which carries the
 * `aws.cognito.signin.user.admin` scope). On success the existing session
 * remains valid. Throws AuthError with a friendly message on failure.
 */
export async function changePassword(previousPassword: string, proposedPassword: string): Promise<void> {
  if (!authConfig.configured) {
    throw new AuthError('Cognito is not configured (missing VITE_COGNITO_REGION / VITE_COGNITO_CLIENT_ID).')
  }
  // Ensure we hold a fresh AccessToken (ensureAuth refreshes Id + Access).
  await ensureAuth()
  const token = accessToken
  if (!token) {
    throw new AuthError('Not signed in. Please sign in again before changing your password.')
  }

  const endpoint = `https://cognito-idp.${REGION}.amazonaws.com/`
  let res: Response
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': 'AWSCognitoIdentityProviderService.ChangePassword',
      },
      body: JSON.stringify({
        AccessToken: token,
        PreviousPassword: previousPassword,
        ProposedPassword: proposedPassword,
      }),
    })
  } catch {
    throw new AuthError('Could not reach the sign-in service. Check your connection.')
  }

  if (res.ok) return

  let data: { message?: string; __type?: string } = {}
  try {
    data = await res.json()
  } catch {
    throw new AuthError(`Password change failed (${res.status}).`)
  }
  const type = (data.__type ?? '').split('#').pop() ?? ''
  switch (type) {
    case 'NotAuthorizedException':
      throw new AuthError('Current password is incorrect.')
    case 'InvalidPasswordException':
    case 'InvalidParameterException':
      throw new AuthError(
        data.message ||
          'New password does not meet the requirements (min 12 chars, upper, lower, number and symbol).',
      )
    case 'PasswordHistoryPolicyViolationException':
      throw new AuthError('New password matches a previous password. Choose a different one.')
    case 'LimitExceededException':
    case 'TooManyRequestsException':
      throw new AuthError('Too many attempts. Please wait a moment and try again.')
    default:
      throw new AuthError(data.message || type || `Password change failed (${res.status}).`)
  }
}

/** Clear the stored session (in-memory tokens + persisted refresh token). */
export function logout(): void {
  storeTokens({ idToken: null, accessToken: null })
  writeRefreshToken(null)
}
