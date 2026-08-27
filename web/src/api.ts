// Typed Simulation_API client. Conforms to DESIGN.md §5 (base path /v1,
// envelope { ok, data|error }, Cognito JWT via Authorization: Bearer).
//
// Two modes:
//   - live:  talks to VITE_API_BASE_URL. Authenticates via src/auth.ts, sending
//            the Cognito IdToken as `Authorization: Bearer <idToken>`. The token
//            is read dynamically (never baked into the build) so it is always
//            fresh; on a 401 we re-authenticate once and retry.
//   - mock:  VITE_MOCK=1 — served entirely by src/mock.ts (no network, no auth).

import type {
  AgentAction,
  AgentDetail,
  ApiEnvelope,
  ControlCommand,
  ConversationItem,
  DecisionTrail,
  EventEntry,
  LocationDetail,
  LocationItem,
  NeedLevels,
  SimState,
  SimStatus,
  StateAgent,
} from './types'
import { mockBackend } from './mock'
import { ensureAuth, getIdToken } from './auth'

const MOCK = import.meta.env.VITE_MOCK === '1'
const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const SIM_ID = import.meta.env.VITE_SIM_ID ?? 'melb'

export const config = {
  mock: MOCK,
  baseUrl: BASE_URL,
  simId: SIM_ID,
}

/** Error carrying the API's returned status + message (for R15.12). */
export class ApiRequestError extends Error {
  status?: SimStatus
  code?: string
  rejectedCommand?: string
  constructor(message: string, opts?: { status?: SimStatus; code?: string; rejectedCommand?: string }) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = opts?.status
    this.code = opts?.code
    this.rejectedCommand = opts?.rejectedCommand
  }
}

/** Build request headers with the current (dynamic) IdToken when available. */
export function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getIdToken()
  if (token) h['Authorization'] = `Bearer ${token}`
  return h
}

/** Headers containing only the bearer token (for asset image fetches). */
export function bearerHeader(): Record<string, string> {
  const token = getIdToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** Base path helper — every path is prefixed with /v1/sim/{simId}. */
function simPath(suffix: string): string {
  return `${BASE_URL}/v1/sim/${encodeURIComponent(SIM_ID)}${suffix}`
}

/**
 * Perform a fetch with the current auth header. On HTTP 401 we re-authenticate
 * once (ensureAuth) and retry with the refreshed token. `init.headers` is
 * rebuilt each attempt so the freshest token is always used.
 */
async function authedFetch(
  path: string,
  init: Omit<RequestInit, 'headers'> & { headers?: Record<string, string> },
  signal?: AbortSignal,
): Promise<Response> {
  const build = (): RequestInit => ({
    ...init,
    headers: { ...authHeaders(), ...(init.headers ?? {}) },
    signal,
  })

  let res = await fetch(path, build())
  if (res.status === 401) {
    try {
      await ensureAuth()
    } catch {
      return res // no way to refresh — surface the original 401
    }
    res = await fetch(path, build())
  }
  return res
}

async function request<T>(
  path: string,
  init: Omit<RequestInit, 'headers'> & { headers?: Record<string, string> },
  signal?: AbortSignal,
): Promise<T> {
  const res = await authedFetch(path, init, signal)
  let body: ApiEnvelope<T> | undefined
  try {
    body = (await res.json()) as ApiEnvelope<T>
  } catch {
    // Non-JSON (e.g. gateway error). Treat as failure.
    throw new ApiRequestError(`Request failed (${res.status})`)
  }
  if (!body.ok) {
    throw new ApiRequestError(body.error?.message ?? 'Request rejected', {
      status: body.error?.status,
      code: body.error?.code,
      rejectedCommand: body.error?.rejectedCommand,
    })
  }
  return body.data
}

// ---------------------------------------------------------------------------
// Response normalization
//
// The Simulation_API (see api/index.py) returns a few payloads whose wire shape
// differs from the flattened view-model shapes the components consume:
//   - GET /state   → each agent's `action` is a bare string (the action *type*),
//                    with `route` carried as a sibling field.
//   - GET /locations → data is wrapped as { locations: [...] } (not a bare array).
//   - GET /agents/{id} → { id, provenance, persona, state, recentEvents } where
//                    needs/cash/legalStatus/etc. live inside `state`.
//   - GET /locations/{id} → { ..., occupancy } detail view.
// These adapters map the server contract onto the client types so components can
// stay simple (and so a single change here fixes every consumer).
// ---------------------------------------------------------------------------

type RawStateAgent = Omit<StateAgent, 'action'> & {
  action?: string | AgentAction | null
}
type RawSimState = Omit<SimState, 'agents'> & { agents?: RawStateAgent[] | null }

/** Coerce an agent's action (string type or object) into an AgentAction. */
function normalizeAgentAction(
  action: string | AgentAction | null | undefined,
  route: StateAgent['route'],
): AgentAction | null {
  if (action == null) return null
  if (typeof action === 'string') {
    // Server sends only the action type; attach the sibling route so the map's
    // travel-path logic (which reads action.type + route) works unchanged.
    return { type: action as AgentAction['type'], route: route ?? null }
  }
  // Already an object: ensure route is populated from the sibling if absent.
  return { ...action, route: action.route ?? route ?? null }
}

function normalizeState(raw: RawSimState): SimState {
  const agents = Array.isArray(raw.agents) ? raw.agents : []
  return {
    simTime: raw.simTime,
    status: raw.status,
    accel: raw.accel,
    conversations: Array.isArray(raw.conversations) ? raw.conversations : [],
    agents: agents.map((a) => ({
      ...a,
      route: a.route ?? null,
      action: normalizeAgentAction(a.action, a.route ?? null),
    })),
  }
}

/** Raw agent-detail wire shape (see api/index.py _handle_agent_detail). */
interface RawAgentDetail {
  id: string
  provenance?: string
  persona: AgentDetail['persona']
  state?: {
    lat?: number
    lon?: number
    needs?: NeedLevels
    critical?: AgentDetail['critical']
    cash?: number
    employmentStatus?: AgentDetail['employmentStatus']
    legalStatus?: AgentDetail['legalStatus']
    currentAction?: AgentAction | null
  } | null
  recentEvents?: EventEntry[] | null
}

function normalizeAgentDetail(raw: RawAgentDetail): AgentDetail {
  const s = raw.state ?? {}
  return {
    id: raw.id,
    name: raw.persona?.name,
    persona: raw.persona,
    needs: s.needs ?? { hunger: 0, energy: 0, social: 0, fun: 0 },
    critical: s.critical,
    cash: s.cash ?? 0,
    employmentStatus: s.employmentStatus ?? 'unemployed',
    legalStatus: s.legalStatus ?? 'clear',
    currentAction: s.currentAction ?? null,
    lat: s.lat ?? 0,
    lon: s.lon ?? 0,
    recentEvents: Array.isArray(raw.recentEvents) ? raw.recentEvents : [],
  }
}

// ---------------------------------------------------------------------------
// Public API surface. All methods accept an optional AbortSignal.
// ---------------------------------------------------------------------------

export async function getState(signal?: AbortSignal): Promise<SimState> {
  if (MOCK) return mockBackend.getState()
  const raw = await request<RawSimState>(simPath('/state'), { method: 'GET' }, signal)
  return normalizeState(raw)
}

export async function getAgent(agentId: string, signal?: AbortSignal): Promise<AgentDetail> {
  if (MOCK) return mockBackend.getAgent(agentId)
  const raw = await request<RawAgentDetail>(
    simPath(`/agents/${encodeURIComponent(agentId)}`),
    { method: 'GET' },
    signal,
  )
  return normalizeAgentDetail(raw)
}

export async function getLocations(signal?: AbortSignal): Promise<LocationItem[]> {
  if (MOCK) return mockBackend.getLocations()
  const data = await request<{ locations?: LocationItem[] } | LocationItem[]>(
    simPath('/locations'),
    { method: 'GET' },
    signal,
  )
  // Server wraps the array as { locations: [...] }; tolerate a bare array too.
  if (Array.isArray(data)) return data
  return Array.isArray(data?.locations) ? data.locations : []
}

export async function getLocation(locId: string, signal?: AbortSignal): Promise<LocationDetail> {
  if (MOCK) return mockBackend.getLocation(locId)
  const raw = await request<LocationDetail>(
    simPath(`/locations/${encodeURIComponent(locId)}`),
    { method: 'GET' },
    signal,
  )
  return {
    ...raw,
    hours: Array.isArray(raw.hours) ? raw.hours : [],
    presentAgents: Array.isArray(raw.presentAgents) ? raw.presentAgents : [],
  }
}

/**
 * Recent agent-to-agent conversations (transcripts). The endpoint wraps the
 * array as { conversations:[...] } (like /locations), so we unwrap it here.
 * Optional `agentId` filters to conversations a given agent took part in.
 */
export async function getConversations(
  agentId?: string,
  signal?: AbortSignal,
): Promise<ConversationItem[]> {
  if (MOCK) return mockBackend.getConversations(agentId)
  const qs = agentId ? `?agentId=${encodeURIComponent(agentId)}` : ''
  const data = await request<{ conversations?: ConversationItem[] } | ConversationItem[]>(
    simPath(`/conversations${qs}`),
    { method: 'GET' },
    signal,
  )
  if (Array.isArray(data)) return data
  return Array.isArray(data?.conversations) ? data.conversations : []
}

/** A single conversation transcript by id. */
export async function getConversation(
  convId: string,
  signal?: AbortSignal,
): Promise<ConversationItem> {
  if (MOCK) return mockBackend.getConversation(convId)
  return request<ConversationItem>(
    simPath(`/conversations/${encodeURIComponent(convId)}`),
    { method: 'GET' },
    signal,
  )
}

/**
 * The decision "thought process" for a given action event (by seq): the LLM's
 * reasoning plus the perception snapshot. Returns null when no trail exists.
 */
export async function getDecisionTrail(
  actionEventSeq: number,
  signal?: AbortSignal,
): Promise<DecisionTrail | null> {
  if (MOCK) return mockBackend.getDecisionTrail(actionEventSeq)
  try {
    return await request<DecisionTrail>(
      simPath(`/events/decision-trail?actionEventSeq=${encodeURIComponent(String(actionEventSeq))}`),
      { method: 'GET' },
      signal,
    )
  } catch {
    // 404 (no trail) is a normal, non-fatal outcome.
    return null
  }
}

/**
 * Issue a control command. On rejection the API returns { ok:false, error }
 * carrying the current status + message — surfaced as ApiRequestError (R15.12).
 */
export async function control(command: ControlCommand, signal?: AbortSignal): Promise<{ status: SimStatus }> {
  if (MOCK) return mockBackend.control(command)
  return request<{ status: SimStatus }>(
    simPath('/control'),
    { method: 'POST', body: JSON.stringify({ command }) },
    signal,
  )
}

/**
 * Asset image URL for a subject (Agent or Location). The /assets/{subjectId}
 * route is Cognito-protected and returns a 302 redirect to a presigned S3 URL
 * (R16.6). Because a bare <img src> cannot send the Authorization header, the
 * URL must be fetched via useAuthImage (which attaches the bearer token and
 * follows the redirect). In mock mode we synthesize a data URL. Returns null
 * when the caller should use a placeholder.
 */
export function assetUrl(subjectId: string): string | null {
  if (MOCK) return mockBackend.assetUrl(subjectId)
  if (!BASE_URL) return null
  return simPath(`/assets/${encodeURIComponent(subjectId)}`)
}

/**
 * Fetch an asset image with the Authorization header and return a blob object
 * URL for it. The API returns a 302 to a presigned S3 URL; fetch follows the
 * redirect (`redirect: 'follow'`) and yields the image bytes. On a 401 we
 * re-auth once and retry. Callers own the returned object URL and must revoke
 * it (URL.revokeObjectURL) when done. Returns null in mock mode / when no URL.
 */
export async function fetchAssetObjectUrl(subjectId: string, signal?: AbortSignal): Promise<string | null> {
  if (MOCK) return mockBackend.assetUrl(subjectId)
  const url = assetUrl(subjectId)
  if (!url) return null

  const doFetch = () =>
    fetch(url, { method: 'GET', headers: bearerHeader(), redirect: 'follow', signal })

  let res = await doFetch()
  if (res.status === 401) {
    await ensureAuth()
    res = await doFetch()
  }
  if (!res.ok) {
    throw new ApiRequestError(`Asset fetch failed (${res.status})`)
  }
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}
