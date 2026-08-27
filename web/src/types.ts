// Types mirroring DESIGN.md §4 (payloads) and §5 (API response shapes).
// The Simulation_API wraps every response as { ok, data } | { ok:false, error }.

export type SimStatus = 'running' | 'paused' | 'stopped'
export type ControlCommand = 'start' | 'pause' | 'resume' | 'stop'

export type LocationCategory =
  | 'residence'
  | 'workplace'
  | 'food'
  | 'retail'
  | 'leisure'
  | 'transit'
  | 'civic'

export type LocationStatus = 'open' | 'closed' | 'at_capacity'

export type EmploymentStatus = 'employed' | 'unemployed' | 'suspended'
export type LegalStatus = 'clear' | 'suspected' | 'charged' | 'detained'

export type ActionType =
  | 'sleep'
  | 'eat'
  | 'work'
  | 'travel'
  | 'socialise'
  | 'shop'
  | 'leisure'
  | 'commit_crime'
  | 'idle'

/** WGS84 coordinate as [lat, lon] (Leaflet order). */
export type LatLon = [number, number]

/** API envelope. */
export type ApiEnvelope<T> = { ok: true; data: T } | { ok: false; error: ApiError }
export interface ApiError {
  message: string
  status?: SimStatus
  code?: string
  rejectedCommand?: string
}

/** A route coordinate as delivered by the API: {lat, lon}. */
export interface RoutePoint {
  lat: number
  lon: number
}

/** An agent action summary (as embedded in state / detail). */
export interface AgentAction {
  type: ActionType
  targetType?: 'location' | 'agent'
  targetId?: string
  expectedDurationMin?: number
  startedSimTime?: string
  progress?: number
  route?: RoutePoint[] | null
  crimeType?: string
}

/** One agent entry from GET /v1/sim/{simId}/state (§5). */
export interface StateAgent {
  id: string
  name: string
  lat: number
  lon: number
  action: AgentAction | null
  route?: RoutePoint[] | null
  legal: LegalStatus
  employment: EmploymentStatus
}

/** One conversation entry from GET /state. */
export interface StateConversation {
  participants: string[]
  locationId: string
}

/** GET /v1/sim/{simId}/state response payload (§5). */
export interface SimState {
  simTime: string
  status: SimStatus
  accel: number
  agents: StateAgent[]
  conversations: StateConversation[]
}

/** Opening hours entry (Mon..Sun, index 0 = Monday). */
export interface HoursEntry {
  open: string
  close: string
}

/** Location payload (§4) plus derived status / present agents from the API. */
export interface LocationItem {
  id: string
  name: string
  category: LocationCategory
  lat: number
  lon: number
  capacity: number
  hours: HoursEntry[]
  price?: number
  isDetentionFacility?: boolean
  status?: LocationStatus
  presentAgents?: { id: string; name: string }[]
}

/** One event-log entry (§4). */
export interface EventEntry {
  seq: number
  simTime: string
  realTime?: string
  category: string
  agents: string[]
  locationId?: string | null
  description: string
}

/** Need levels (§4). */
export interface NeedLevels {
  hunger: number
  energy: number
  social: number
  fun: number
}

/** Full agent detail from GET /v1/sim/{simId}/agents/{agentId} (R15.6). */
export interface AgentDetail {
  id: string
  name: string
  persona: {
    name: string
    age: number
    occupation: string
    traits: string[]
    background: string
    homeLocationId?: string
  }
  needs: NeedLevels
  critical?: Partial<Record<keyof NeedLevels, boolean>>
  cash: number
  employmentStatus: EmploymentStatus
  legalStatus: LegalStatus
  currentAction: AgentAction | null
  lat: number
  lon: number
  recentEvents: EventEntry[]
}

/** Full location detail from GET /v1/sim/{simId}/locations/{locId} (R15.7). */
export interface LocationDetail extends LocationItem {
  status: LocationStatus
  presentAgents: { id: string; name: string }[]
}
