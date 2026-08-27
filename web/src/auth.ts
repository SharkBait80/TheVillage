// Cognito authentication (in-app, always-fresh JWT).
//
// Baking a static token into the build fails because Cognito ID tokens expire
// in ~1h. Instead we authenticate at runtime against the Cognito Identity
// Provider REST API (InitiateAuth, USER_PASSWORD_AUTH flow) using plain fetch —
// no `amazon-cognito-identity-js` dependency.
//
// The resulting IdToken is what the API Gateway Cognito JWT authorizer accepts
// as `Authorization: Bearer <idToken>`. We keep it in memory + localStorage and
// transparently re-authenticate when it is missing or about to expire.
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

const STORAGE_KEY = 'village.idToken'
/** Re-auth when within this many ms of expiry. */
const REFRESH_SKEW_MS = 5 * 60 * 1000

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

// In-memory copy of the current token (source of truth during a session).
let idToken: string | null = readStoredToken()
// Guards against overlapping re-auth calls.
let inFlight: Promise<string> | null = null
// Last credentials used, so ensureAuth() can silently refresh.
let lastUsername: string | null = null
let lastPassword: string | null = null

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function storeToken(token: string | null): void {
  idToken = token
  try {
    if (token) window.localStorage.setItem(STORAGE_KEY, token)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Ignore storage failures (e.g. private mode); memory copy still works.
  }
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
    AuthenticationResult?: { IdToken?: string; ExpiresIn?: number }
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

  lastUsername = username
  lastPassword = password
  storeToken(token)
  return token
}

/**
 * Ensure we hold a fresh token. Returns it, re-authenticating with the last
 * used credentials (or the configured operator creds) when needed. Concurrent
 * callers share a single in-flight request. Throws AuthError when no valid
 * credentials are available to refresh with.
 */
export async function ensureAuth(): Promise<string> {
  if (isTokenFresh(idToken)) return idToken as string
  if (inFlight) return inFlight

  const username = lastUsername ?? (OPERATOR_USER || null)
  const password = lastPassword ?? (OPERATOR_PASS || null)
  if (!username || !password) {
    throw new AuthError('Session expired. Please sign in again.')
  }

  inFlight = login(username, password).finally(() => {
    inFlight = null
  })
  return inFlight
}

/**
 * Attempt an automatic operator login on app load when VITE_OPERATOR_USER /
 * VITE_OPERATOR_PASS are configured. Resolves true on success, false otherwise
 * (never throws) so the UI can fall back to the login screen.
 */
export async function tryAutoLogin(): Promise<boolean> {
  if (isTokenFresh(idToken)) return true
  if (!authConfig.hasOperatorCreds) return false
  try {
    await login(OPERATOR_USER, OPERATOR_PASS)
    return true
  } catch {
    return false
  }
}

/** Clear the stored session. */
export function logout(): void {
  lastUsername = null
  lastPassword = null
  storeToken(null)
}
